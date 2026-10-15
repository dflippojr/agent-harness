"""Held-out hard tasks for the harness-learning experiment (#673, #674).

The proposer in #673 revises the harness from the canary's hard tasks, so its held-out set needs tasks it never
sees. These six candidates are modeled on domain logic from the owner's projects (league draws and money,
recurring-charge detection, CSV re-import, ordered event consumption, bank CSV shapes), rewritten as standalone
tasks with synthetic data. Each follows the `tasks_hard.py` pattern: a prompt, starting files, a hidden checker
the agent never sees and a reference `solve`.

Keep them out of `canary.yaml` and out of anything the proposer reads. Calibration (3 attempts each with the
production model, through `Manager`): `python -m bakeoff.canary --heldout --repeats 3`; see docs/phase0-results.md.
"""

from __future__ import annotations

from .tasks import Context, Task, pytest_ok, unchanged, write
from .tasks_hard import HARD_LIMITS


def _visible_then_hidden(ctx: Context, visible: str, hidden_rel: str, hidden: str) -> tuple[bool, str]:
    if not unchanged(ctx, visible):
        return False, "tests were modified"
    ok, note = pytest_ok(ctx, visible)
    if not ok:
        return False, f"visible tests: {note}"
    write(ctx, hidden_rel, hidden)
    return pytest_ok(ctx, hidden_rel)


# --- 1. cali_draw (disc golf doubles draw) ----------------------------------
# A first draft that ignores most of the docstring. The hidden tests replay the specified shuffle exactly.

DRAW_DOC = '''"""Doubles draw for league night.

    Players sign up in an A pool or a B pool, and each team pairs an A player with a B player.

    - `players` maps each player's name to their pool, "A" or "B". Any other pool, or fewer than 2 players,
      raises ValueError.
    - The draw depends only on the players, their pools, `seed` and `previous_cali`, never on the order of
      `players`. Build each pool as a list sorted by name, then make one `random.Random(seed)` and call its
      `shuffle` on the A list and then on the B list, once each and in that order.
    - Cali: when the total number of players is odd, one player is the cali (plays alone and throws twice). The
      cali comes from the larger pool: the first player in that pool's shuffled order who is not
      `previous_cali`. Nobody is cali two events running. With an even total there is no cali.
    - Teams: after taking out the cali, pair the A list with the B list position by position (A[0] with B[0],
      and so on). The players left over all come from one pool; pair them in their shuffled order
      (rest[0] with rest[1], rest[2] with rest[3], ...).
    - `Draw.teams` lists the mixed teams first, each as (A player, B player), then the same-pool teams.
      `Draw.cali` is the cali's name, or None.
    """'''

DRAW_DRAFT = '''import random
from dataclasses import dataclass


@dataclass
class Draw:
    teams: list[tuple[str, str]]
    cali: str | None


def draw(players: dict[str, str], seed: int, previous_cali: str | None = None) -> Draw:
    ''' + DRAW_DOC + '''
    rng = random.Random(seed)
    a_pool = [name for name, pool in players.items() if pool == "A"]
    b_pool = [name for name, pool in players.items() if pool == "B"]
    rng.shuffle(a_pool)
    rng.shuffle(b_pool)
    cali = None
    if len(players) % 2 == 1:
        cali = rng.choice(sorted(players))
        (a_pool if players[cali] == "A" else b_pool).remove(cali)
    return Draw(list(zip(a_pool, b_pool)), cali)
'''

DRAW_TESTS = '''from league.draw import draw


def placed(result):
    return sorted([p for team in result.teams for p in team] + ([result.cali] if result.cali else []))


def test_even_balanced_pools_make_mixed_teams():
    players = {"ana": "A", "ben": "A", "cy": "B", "dee": "B"}
    result = draw(players, seed=7)
    assert result.cali is None
    assert len(result.teams) == 2
    for a, b in result.teams:
        assert players[a] == "A" and players[b] == "B"


def test_unbalanced_pools_place_everyone():
    players = {"ana": "A", "ben": "A", "cal": "A", "dot": "A", "eve": "B", "fay": "B"}
    assert placed(draw(players, seed=3)) == sorted(players)


def test_odd_count_has_a_cali():
    players = {"ana": "A", "ben": "A", "cal": "A", "eve": "B", "fay": "B"}
    result = draw(players, seed=1)
    assert result.cali in players
    assert placed(result) == sorted(players)


def test_same_seed_same_draw():
    players = {f"p{i}": "AB"[i % 2] for i in range(8)}
    assert draw(players, 42) == draw(players, 42)
'''

DRAW_HIDDEN = '''import random

import pytest

from league.draw import draw


def expected(players, seed, previous_cali=None):
    a = sorted(n for n, p in players.items() if p == "A")
    b = sorted(n for n, p in players.items() if p == "B")
    rng = random.Random(seed)
    rng.shuffle(a)
    rng.shuffle(b)
    cali = None
    if len(players) % 2:
        larger = a if len(a) > len(b) else b
        cali = next(n for n in larger if n != previous_cali)
        larger.remove(cali)
    teams = list(zip(a, b))
    rest = a[len(teams):] + b[len(teams):]
    teams += [(rest[i], rest[i + 1]) for i in range(0, len(rest), 2)]
    return teams, cali


def roster(n_a, n_b):
    return {**{f"a{i:02d}": "A" for i in range(n_a)}, **{f"b{i:02d}": "B" for i in range(n_b)}}


def got(result):
    return [tuple(t) for t in result.teams], result.cali


SIZES = [(4, 4), (5, 4), (4, 5), (7, 2), (2, 7), (3, 0), (0, 4), (6, 1), (1, 2), (10, 9)]


@pytest.mark.parametrize("n_a,n_b", SIZES)
@pytest.mark.parametrize("seed", [0, 1, 99, 2026])
def test_hidden_exact_draw(n_a, n_b, seed):
    players = roster(n_a, n_b)
    assert got(draw(players, seed)) == expected(players, seed)


def test_hidden_input_order_does_not_matter():
    players = roster(6, 5)
    flipped = dict(reversed(list(players.items())))
    assert got(draw(flipped, 11)) == got(draw(players, 11))


@pytest.mark.parametrize("seed", range(25))
def test_hidden_cali_rotates(seed):
    players = roster(5, 4)
    first = draw(players, seed).cali
    second = draw(players, seed, previous_cali=first)
    assert second.cali is not None and second.cali != first
    assert got(second) == expected(players, seed, first)


def test_hidden_previous_cali_from_the_other_pool_is_irrelevant():
    players = roster(2, 5)
    assert got(draw(players, 4, previous_cali="a00")) == expected(players, 4)


def test_hidden_even_total_has_no_cali():
    assert draw(roster(3, 3), 5, previous_cali="a00").cali is None


@pytest.mark.parametrize("n_a,n_b", SIZES)
def test_hidden_pool_balance(n_a, n_b):
    players = roster(n_a, n_b)
    result = draw(players, 8)
    seated = [p for team in result.teams for p in team] + ([result.cali] if result.cali else [])
    assert sorted(seated) == sorted(players)
    mixed = sum(1 for a, b in result.teams if players[a] != players[b])
    left_a = n_a - (result.cali is not None and players[result.cali] == "A")
    left_b = n_b - (result.cali is not None and players[result.cali] == "B")
    assert mixed == min(left_a, left_b)


@pytest.mark.parametrize("players", [{}, {"solo": "A"}, {"x": "A", "y": "C"}])
def test_hidden_invalid(players):
    with pytest.raises(ValueError):
        draw(players, 1)
'''

DRAW_SOLUTION = '''import random
from dataclasses import dataclass


@dataclass
class Draw:
    teams: list[tuple[str, str]]
    cali: str | None


def draw(players: dict[str, str], seed: int, previous_cali: str | None = None) -> Draw:
    if len(players) < 2:
        raise ValueError("a draw needs at least two players")
    unknown = sorted(name for name, pool in players.items() if pool not in ("A", "B"))
    if unknown:
        raise ValueError(f"unknown pool for {unknown}")
    a_pool = sorted(name for name, pool in players.items() if pool == "A")
    b_pool = sorted(name for name, pool in players.items() if pool == "B")
    rng = random.Random(seed)
    rng.shuffle(a_pool)
    rng.shuffle(b_pool)
    cali = None
    if len(players) % 2:
        larger = a_pool if len(a_pool) > len(b_pool) else b_pool
        cali = next(name for name in larger if name != previous_cali)
        larger.remove(cali)
    teams = list(zip(a_pool, b_pool))
    rest = a_pool[len(teams):] + b_pool[len(teams):]
    teams += [(rest[i], rest[i + 1]) for i in range(0, len(rest), 2)]
    return Draw(teams, cali)
'''


def draw_check(ctx: Context) -> tuple[bool, str]:
    return _visible_then_hidden(ctx, "tests/test_draw.py", "tests/_hidden_test_draw.py", DRAW_HIDDEN)


def draw_solve(ctx: Context) -> str:
    write(ctx, "league/draw.py", DRAW_SOLUTION)
    return "rewrote draw to the spec"


# --- 2. ace_pot_ledger ------------------------------------------------------
# Five bugs against the docstring: rounding, duplicate aces, unclaimed CTP, the cap overflow and season reset.

LEDGER_DOC = '''"""League-night money.

    Each entry pays `entry_fee_cents`. Of each entry fee, `ace_cents` goes to the ace pot, `ctp_cents` to the
    closest-to-the-pin (CTP) prize, and the rest to the night's payout.

    record_night(entries, aces=(), ctp_winner=None) -> NightResult
    - The ace pot carries over from night to night until someone hits an ace.
    - Tonight's ace money joins the pot before any ace is paid, so an ace tonight wins it too.
    - If nobody wins CTP (`ctp_winner` is None), tonight's CTP money goes into the ace pot and the result's
      `ctp_cents` is 0.
    - Cap: when `pot_cap_cents` is set, the pot never holds more than that. Money that would take it over the cap
      (ace money or unclaimed CTP money) goes to tonight's payout instead.
    - Aces: `aces` names the player for each ace hit tonight; a player with two aces appears twice and gets two
      shares. The pot is split into equal whole-cent shares, one per ace: share = pot // number of aces. The cents
      left over (pot % number of aces) stay in the pot for next week. `ace_payouts` maps each player to the total
      they won.
    - `pot_after_cents` is the pot once tonight is settled. Every night is appended to `history`.
    - Negative `entries` raises ValueError.

    reset_season() -> int: ends the season. Returns the unclaimed pot (it is donated to the course), empties the
    pot and clears `history`.
    """'''

LEDGER_DRAFT = '''from dataclasses import dataclass


@dataclass
class NightResult:
    payout_cents: int
    ctp_cents: int
    ace_payouts: dict[str, int]
    pot_after_cents: int


class Ledger:
    ''' + LEDGER_DOC + '''

    def __init__(self, entry_fee_cents: int = 500, ace_cents: int = 100, ctp_cents: int = 100,
                 pot_cap_cents: int | None = None):
        if ace_cents + ctp_cents > entry_fee_cents:
            raise ValueError("ace and CTP shares exceed the entry fee")
        self.entry_fee_cents = entry_fee_cents
        self.ace_cents = ace_cents
        self.ctp_cents = ctp_cents
        self.pot_cap_cents = pot_cap_cents
        self.pot_cents = 0
        self.history: list[NightResult] = []

    def record_night(self, entries: int, aces=(), ctp_winner: str | None = None) -> NightResult:
        if entries < 0:
            raise ValueError("entries must be >= 0")
        payout = entries * (self.entry_fee_cents - self.ace_cents - self.ctp_cents)
        ctp = entries * self.ctp_cents
        self.pot_cents += entries * self.ace_cents
        if ctp_winner is None:
            payout += ctp
            ctp = 0
        if self.pot_cap_cents is not None:
            self.pot_cents = min(self.pot_cents, self.pot_cap_cents)
        ace_payouts = {}
        if aces:
            share = round(self.pot_cents / len(aces))
            for player in aces:
                ace_payouts[player] = share
            self.pot_cents = 0
        result = NightResult(payout, ctp, ace_payouts, self.pot_cents)
        self.history.append(result)
        return result

    def reset_season(self) -> int:
        self.pot_cents = 0
'''

LEDGER_TESTS = '''from league.ledger import Ledger


def test_plain_night():
    result = Ledger().record_night(10, ctp_winner="kim")
    assert (result.payout_cents, result.ctp_cents, result.pot_after_cents) == (3000, 1000, 1000)
    assert result.ace_payouts == {}


def test_pot_carries_over():
    ledger = Ledger()
    ledger.record_night(10, ctp_winner="kim")
    ledger.record_night(8, ctp_winner="lu")
    assert ledger.pot_cents == 1800


def test_single_ace_takes_the_pot_including_tonight():
    ledger = Ledger()
    ledger.record_night(10, ctp_winner="kim")
    result = ledger.record_night(5, aces=["jo"], ctp_winner="lu")
    assert result.ace_payouts == {"jo": 1500}
    assert result.pot_after_cents == 0


def test_two_aces_split_and_the_odd_cent_stays():
    ledger = Ledger(ace_cents=75)
    result = ledger.record_night(7, aces=["ana", "bo"], ctp_winner="kim")
    assert result.ace_payouts == {"ana": 262, "bo": 262}
    assert result.pot_after_cents == 1
'''

LEDGER_HIDDEN = '''import random

import pytest

from league.ledger import Ledger


def test_hidden_three_way_split_carries_remainder():
    ledger = Ledger(entry_fee_cents=600, ace_cents=110, ctp_cents=100)
    ledger.record_night(9, ctp_winner="x")
    result = ledger.record_night(2, aces=["a", "b", "c"], ctp_winner="y")
    assert result.ace_payouts == {"a": 403, "b": 403, "c": 403}
    assert result.pot_after_cents == 1
    assert ledger.record_night(1, ctp_winner="z").pot_after_cents == 111


def test_hidden_same_player_two_aces_gets_two_shares():
    ledger = Ledger()
    ledger.record_night(9, ctp_winner="x")
    result = ledger.record_night(0, aces=["sam", "lee", "sam"])
    assert result.ace_payouts == {"sam": 600, "lee": 300}
    assert result.pot_after_cents == 0


def test_hidden_small_pot_many_aces():
    ledger = Ledger(entry_fee_cents=10, ace_cents=1, ctp_cents=1)
    result = ledger.record_night(2, aces=["a", "b", "c"], ctp_winner="x")
    assert result.ace_payouts == {"a": 0, "b": 0, "c": 0}
    assert result.pot_after_cents == 2


def test_hidden_unclaimed_ctp_goes_to_the_pot():
    ledger = Ledger()
    result = ledger.record_night(10)
    assert (result.payout_cents, result.ctp_cents, result.pot_after_cents) == (3000, 0, 2000)


def test_hidden_cap_overflow_goes_to_payout():
    ledger = Ledger(pot_cap_cents=2500)
    ledger.record_night(20, ctp_winner="x")
    result = ledger.record_night(10, ctp_winner="x")
    assert result.pot_after_cents == 2500
    assert result.payout_cents == 3000 + 500


def test_hidden_cap_with_unclaimed_ctp():
    ledger = Ledger(pot_cap_cents=1500)
    result = ledger.record_night(10)
    assert (result.payout_cents, result.ctp_cents, result.pot_after_cents) == (3500, 0, 1500)
    full = ledger.record_night(4)
    assert (full.payout_cents, full.pot_after_cents) == (1200 + 800, 1500)


def test_hidden_ace_after_cap_pays_the_capped_pot():
    ledger = Ledger(pot_cap_cents=1000)
    ledger.record_night(30, ctp_winner="x")
    result = ledger.record_night(3, aces=["kai"], ctp_winner="x")
    assert result.ace_payouts == {"kai": 1000}
    assert result.payout_cents == 900 + 300


def test_hidden_season_reset():
    ledger = Ledger()
    ledger.record_night(10, ctp_winner="x")
    ledger.record_night(6)
    assert ledger.reset_season() == 2200
    assert ledger.pot_cents == 0 and ledger.history == []
    assert ledger.record_night(1, ctp_winner="x").pot_after_cents == 100
    assert len(ledger.history) == 1


def test_hidden_negative_entries():
    with pytest.raises(ValueError):
        Ledger().record_night(-1)


@pytest.mark.parametrize("seed", range(5))
def test_hidden_money_is_conserved(seed):
    rng = random.Random(seed)
    ledger = Ledger(entry_fee_cents=700, ace_cents=130, ctp_cents=90, pot_cap_cents=rng.choice([None, 2000, 5000]))
    for _ in range(30):
        entries = rng.randint(0, 25)
        aces = rng.choices(["a", "b", "c"], k=rng.choice([0, 0, 0, 1, 2, 3]))
        before = ledger.pot_cents
        r = ledger.record_night(entries, aces=aces, ctp_winner=rng.choice([None, "w"]))
        paid = r.payout_cents + r.ctp_cents + sum(r.ace_payouts.values())
        assert paid + r.pot_after_cents - before == entries * 700
        assert ledger.pot_cents == r.pot_after_cents
        if ledger.pot_cap_cents is not None:
            assert r.pot_after_cents <= ledger.pot_cap_cents
'''

LEDGER_SOLUTION = '''from dataclasses import dataclass


@dataclass
class NightResult:
    payout_cents: int
    ctp_cents: int
    ace_payouts: dict[str, int]
    pot_after_cents: int


class Ledger:
    def __init__(self, entry_fee_cents: int = 500, ace_cents: int = 100, ctp_cents: int = 100,
                 pot_cap_cents: int | None = None):
        if ace_cents + ctp_cents > entry_fee_cents:
            raise ValueError("ace and CTP shares exceed the entry fee")
        self.entry_fee_cents = entry_fee_cents
        self.ace_cents = ace_cents
        self.ctp_cents = ctp_cents
        self.pot_cap_cents = pot_cap_cents
        self.pot_cents = 0
        self.history: list[NightResult] = []

    def _add_to_pot(self, cents: int) -> int:
        """Add to the pot up to the cap; return what didn't fit."""
        room = cents if self.pot_cap_cents is None else max(0, self.pot_cap_cents - self.pot_cents)
        kept = min(room, cents)
        self.pot_cents += kept
        return cents - kept

    def record_night(self, entries: int, aces=(), ctp_winner: str | None = None) -> NightResult:
        if entries < 0:
            raise ValueError("entries must be >= 0")
        payout = entries * (self.entry_fee_cents - self.ace_cents - self.ctp_cents)
        ctp = entries * self.ctp_cents
        to_pot = entries * self.ace_cents
        if ctp_winner is None:
            to_pot, ctp = to_pot + ctp, 0
        payout += self._add_to_pot(to_pot)
        ace_payouts: dict[str, int] = {}
        if aces:
            share, self.pot_cents = divmod(self.pot_cents, len(aces))
            for player in aces:
                ace_payouts[player] = ace_payouts.get(player, 0) + share
        result = NightResult(payout, ctp, ace_payouts, self.pot_cents)
        self.history.append(result)
        return result

    def reset_season(self) -> int:
        unclaimed, self.pot_cents = self.pot_cents, 0
        self.history.clear()
        return unclaimed
'''


def ledger_check(ctx: Context) -> tuple[bool, str]:
    return _visible_then_hidden(ctx, "tests/test_ledger.py", "tests/_hidden_test_ledger.py", LEDGER_HIDDEN)


def ledger_solve(ctx: Context) -> str:
    write(ctx, "league/ledger.py", LEDGER_SOLUTION)
    return "fixed the ledger"


# --- 3. recurring_charges ---------------------------------------------------
# Implement from a spec: monthly series, month-end billing days, missed vs cancelled, and drift vs price change.

RECUR_STUB = '''"""Recurring-charge alerts.

recurring_alerts(transactions, today) -> list[Alert]

`transactions` is a list of Transaction in any order; amounts are positive cents.

1. Merchants are compared after collapsing every run of whitespace to one space, stripping the ends and casefolding
   ("NETFLIX.COM  " and "netflix.com" are the same merchant). Alert.merchant is that normalized name.
2. A merchant's charges form a monthly series when there are at least 3 of them and, sorted by date, every
   consecutive pair is 1 or 2 calendar months apart (compare year and month only: Jan 31 to Feb 1 is 1 month;
   2 months means a skipped month, and the series continues) and the later amount is within 25% of the earlier
   one (abs(new - old) * 100 <= 25 * old). A merchant whose charges break any of these rules is not a series and
   gets no alerts.
3. Billing day: the day of the month of the series' first charge. In a month too short for it the charge is
   expected on the month's last day (a series that started on the 31st is expected on Feb 28, or Feb 29 in a
   leap year, and on Apr 30).
4. Missed: the next charge is expected in the month after the last charge, on the billing day. A charge is
   overdue when `today` is more than GRACE_DAYS days after its expected date. If the next charge is overdue,
   emit Alert(merchant, "missed", expected_date=<its expected date>). If the charge expected the month after that
   is overdue too, the series is cancelled: it gets no alerts at all.
5. Price change: if the last charge differs from the one before it by more than 5% (abs(new - old) * 100 > 5 * old),
   emit Alert(merchant, "price_change", old_cents=<previous amount>, new_cents=<last amount>). Smaller drift is not
   a price change.
6. A series can get both alerts. Return all alerts sorted by (merchant, kind).
"""

from dataclasses import dataclass
from datetime import date

GRACE_DAYS = 5


@dataclass
class Transaction:
    date: date
    merchant: str
    amount_cents: int


@dataclass
class Alert:
    merchant: str
    kind: str  # "missed" or "price_change"
    expected_date: date | None = None
    old_cents: int | None = None
    new_cents: int | None = None


def recurring_alerts(transactions: list[Transaction], today: date) -> list[Alert]:
    raise NotImplementedError
'''

RECUR_HIDDEN = '''from datetime import date

import pytest

from finance.recurring import Alert, Transaction, recurring_alerts


def series(merchant, dates, amounts):
    if isinstance(amounts, int):
        amounts = [amounts] * len(dates)
    return [Transaction(date.fromisoformat(d), merchant, a) for d, a in zip(dates, amounts)]


MONTHLY = ["2026-01-10", "2026-02-10", "2026-03-10"]


def test_hidden_on_time_series_has_no_alert():
    assert recurring_alerts(series("Gym", MONTHLY, 4500), date(2026, 4, 2)) == []


@pytest.mark.parametrize("today,alert", [(date(2026, 4, 15), False), (date(2026, 4, 16), True)])
def test_hidden_grace_boundary(today, alert):
    got = recurring_alerts(series("Gym", MONTHLY, 4500), today)
    assert got == ([Alert("gym", "missed", expected_date=date(2026, 4, 10))] if alert else [])


def test_hidden_month_end_to_february():
    tx = series("Stream Co", ["2025-10-31", "2025-11-30", "2025-12-31", "2026-01-31"], 1599)
    assert recurring_alerts(tx, date(2026, 3, 5)) == []
    assert recurring_alerts(tx, date(2026, 3, 6)) == [Alert("stream co", "missed", expected_date=date(2026, 2, 28))]


def test_hidden_month_end_leap_year():
    tx = series("Stream Co", ["2027-10-31", "2027-11-30", "2027-12-31", "2028-01-31"], 1599)
    assert recurring_alerts(tx, date(2028, 3, 6)) == [Alert("stream co", "missed", expected_date=date(2028, 2, 29))]


def test_hidden_billing_day_comes_from_the_first_charge():
    tx = series("Cloud", ["2026-01-31", "2026-02-28", "2026-03-31"], 299)
    assert recurring_alerts(tx, date(2026, 5, 6)) == [Alert("cloud", "missed", expected_date=date(2026, 4, 30))]
    tx = series("Cloud", ["2026-05-30", "2026-06-30", "2026-07-31"], 299)
    assert recurring_alerts(tx, date(2026, 9, 5)) == [Alert("cloud", "missed", expected_date=date(2026, 8, 30))]


def test_hidden_year_boundary():
    tx = series("Paper", ["2025-10-03", "2025-11-03", "2025-12-03"], 1200)
    assert recurring_alerts(tx, date(2026, 1, 9)) == [Alert("paper", "missed", expected_date=date(2026, 1, 3))]


def test_hidden_skipped_month_keeps_the_series():
    tx = series("Gym", ["2026-01-05", "2026-03-05", "2026-04-05"], 4500)
    assert recurring_alerts(tx, date(2026, 5, 11)) == [Alert("gym", "missed", expected_date=date(2026, 5, 5))]


def test_hidden_three_month_gap_is_not_a_series():
    tx = series("Gym", ["2026-01-05", "2026-04-05", "2026-05-05"], 4500)
    assert recurring_alerts(tx, date(2026, 6, 30)) == []


@pytest.mark.parametrize("today,expected", [
    (date(2026, 5, 15), [Alert("gym", "missed", expected_date=date(2026, 4, 10)),
                         Alert("gym", "price_change", old_cents=4500, new_cents=5200)]),
    (date(2026, 5, 16), []),
])
def test_hidden_cancelled_series_is_quiet(today, expected):
    tx = series("Gym", MONTHLY, [4500, 4500, 5200])
    assert recurring_alerts(tx, today) == expected


@pytest.mark.parametrize("old,new,alert", [
    (999, 1099, True), (1000, 1050, False), (1000, 1051, True), (1000, 1030, False), (1000, 940, True),
    (1000, 951, False),
])
def test_hidden_price_change_threshold(old, new, alert):
    tx = series("Music", MONTHLY, [old, old, new])
    got = recurring_alerts(tx, date(2026, 3, 20))
    assert got == ([Alert("music", "price_change", old_cents=old, new_cents=new)] if alert else [])


def test_hidden_drift_compares_the_last_two_only():
    tx = series("Power", ["2026-01-02", "2026-02-02", "2026-03-02", "2026-04-02"], [1000, 1040, 1080, 1120])
    assert recurring_alerts(tx, date(2026, 4, 20)) == []


def test_hidden_big_jump_breaks_the_series():
    tx = series("Music", MONTHLY, [1000, 1000, 1300])
    assert recurring_alerts(tx, date(2026, 3, 20)) == []


def test_hidden_both_alerts_and_sorting():
    tx = series("  Zeta   Video ", MONTHLY, [800, 800, 900]) + series("alpha", MONTHLY, 500)
    got = recurring_alerts(list(reversed(tx)), date(2026, 4, 30))
    assert got == [
        Alert("alpha", "missed", expected_date=date(2026, 4, 10)),
        Alert("zeta video", "missed", expected_date=date(2026, 4, 10)),
        Alert("zeta video", "price_change", old_cents=800, new_cents=900),
    ]


def test_hidden_merchant_normalization_merges_series():
    tx = [Transaction(date(2026, 1, 8), "Spotify  USA", 1199), Transaction(date(2026, 2, 8), "spotify usa", 1199),
          Transaction(date(2026, 3, 8), "SPOTIFY\\tUSA ", 1199)]
    assert recurring_alerts(tx, date(2026, 4, 14)) == [Alert("spotify usa", "missed", expected_date=date(2026, 4, 8))]


@pytest.mark.parametrize("dates", [["2026-01-10", "2026-02-10"], ["2026-01-10", "2026-01-25", "2026-02-10"]])
def test_hidden_not_enough_for_a_series(dates):
    assert recurring_alerts(series("Gym", dates, 4500), date(2026, 6, 1)) == []
'''

RECUR_SOLUTION = RECUR_STUB.replace('''def recurring_alerts(transactions: list[Transaction], today: date) -> list[Alert]:
    raise NotImplementedError
''', '''def _merchant(name: str) -> str:
    return " ".join(name.split()).casefold()


def _month(d: date) -> int:
    return d.year * 12 + d.month - 1


def _due(month: int, day: int) -> date:
    year, m = divmod(month, 12)
    return date(year, m + 1, min(day, calendar.monthrange(year, m + 1)[1]))


def _is_series(charges: list[Transaction]) -> bool:
    return len(charges) >= 3 and all(
        1 <= _month(b.date) - _month(a.date) <= 2 and abs(b.amount_cents - a.amount_cents) * 100 <= 25 * a.amount_cents
        for a, b in zip(charges, charges[1:]))


def recurring_alerts(transactions: list[Transaction], today: date) -> list[Alert]:
    by_merchant: dict[str, list[Transaction]] = {}
    for t in transactions:
        by_merchant.setdefault(_merchant(t.merchant), []).append(t)
    grace = timedelta(days=GRACE_DAYS)
    alerts = []
    for merchant, charges in by_merchant.items():
        charges = sorted(charges, key=lambda t: t.date)
        if not _is_series(charges):
            continue
        day, last = charges[0].date.day, _month(charges[-1].date)
        next_due = _due(last + 1, day)
        if today > _due(last + 2, day) + grace:
            continue
        if today > next_due + grace:
            alerts.append(Alert(merchant, "missed", expected_date=next_due))
        old, new = charges[-2].amount_cents, charges[-1].amount_cents
        if abs(new - old) * 100 > 5 * old:
            alerts.append(Alert(merchant, "price_change", old_cents=old, new_cents=new))
    return sorted(alerts, key=lambda a: (a.merchant, a.kind))
''').replace("from datetime import date\n", "import calendar\nfrom datetime import date, timedelta\n")


def recur_check(ctx: Context) -> tuple[bool, str]:
    write(ctx, "tests/_hidden_test_recurring.py", RECUR_HIDDEN)
    return pytest_ok(ctx, "tests/_hidden_test_recurring.py")


def recur_solve(ctx: Context) -> str:
    write(ctx, "finance/recurring.py", RECUR_SOLUTION)
    return "implemented"


# --- 4. reimport_overlap ----------------------------------------------------
# A naive fingerprint matcher: no field separator, weak normalization, recomputed stored fingerprints, set
# matching (loses same-day duplicates), manual rows matched, ambiguity ignoring the account and a single pass.

FP_DRAFT = '''"""Re-import overlap.

When a user imports a bank CSV that overlaps one imported earlier, each incoming row is classified as "duplicate"
(already stored), "ambiguous" (the user must decide) or "new".

fingerprint(row): the SHA-256 hex digest of the UTF-8 encoding of account, date, amount_cents (as a decimal string)
and the normalized description, in that order, joined with a newline ("\\\\n") so field boundaries can't collide
("ab" + "c" must differ from "a" + "bc"). normalize_description casefolds, collapses every run of whitespace
(spaces, tabs, newlines) to one space and strips the ends.

classify(incoming, stored) returns one label per incoming row, in incoming order:
- Manual rows (source "manual") never match imported rows; ignore them entirely.
- Stored import rows match by their stored `fingerprint`, taken from the row as the bank sent it. Users may edit a
  stored row's description later; that must not change matching, so never recompute it from the stored row.
- Each stored row matches at most one incoming row. A CSV can legitimately hold identical rows (two coffees on the
  same day): if it holds three identical rows and two are stored, two are duplicates and one is new.
- Pass 1: each incoming row, in order, whose fingerprint equals that of a stored import row not matched yet is
  "duplicate" and uses up that stored row (the earliest in `stored` order).
- Pass 2: each remaining incoming row, in order, that has a stored import row not matched yet with the same
  account, date and amount_cents (the bank changed the description) is "ambiguous" and uses up that stored row
  (the earliest).
- Everything else is "new".
"""

import hashlib
from dataclasses import dataclass


@dataclass
class Row:
    account: str
    date: str  # ISO yyyy-mm-dd
    amount_cents: int
    description: str


@dataclass
class StoredRow(Row):
    source: str = "import"  # "import" or "manual"
    fingerprint: str | None = None  # set at import time; None for manual rows


def normalize_description(text: str) -> str:
    return text.strip().lower()


def fingerprint(row: Row) -> str:
    raw = row.account + row.date + str(row.amount_cents) + normalize_description(row.description)
    return hashlib.sha256(raw.encode()).hexdigest()


def classify(incoming: list[Row], stored: list[StoredRow]) -> list[str]:
    known = {fingerprint(s) for s in stored}
    labels = []
    for row in incoming:
        if fingerprint(row) in known:
            labels.append("duplicate")
        elif any(s.date == row.date and s.amount_cents == row.amount_cents for s in stored):
            labels.append("ambiguous")
        else:
            labels.append("new")
    return labels
'''

FP_TESTS = '''from finance.fingerprint import Row, StoredRow, classify, fingerprint


def imported(row):
    return StoredRow(row.account, row.date, row.amount_cents, row.description, "import", fingerprint(row))


def test_exact_reimport_is_duplicate():
    row = Row("chk", "2026-03-01", 1250, "Coffee Shop")
    assert classify([row], [imported(row)]) == ["duplicate"]


def test_unseen_row_is_new():
    stored = [imported(Row("chk", "2026-03-01", 1250, "Coffee Shop"))]
    assert classify([Row("chk", "2026-03-02", 800, "Bakery")], stored) == ["new"]


def test_two_coffees_one_stored():
    row = Row("chk", "2026-03-01", 450, "Coffee Shop")
    assert classify([row, row], [imported(row)]) == ["duplicate", "new"]
'''

FP_HIDDEN = '''import hashlib

from finance.fingerprint import Row, StoredRow, classify, fingerprint, normalize_description


def imported(row, description=None):
    return StoredRow(row.account, row.date, row.amount_cents, description or row.description, "import",
                     fingerprint(row))


def test_hidden_fingerprint_format():
    row = Row("chk", "2026-03-01", -1250, "  Coffee\\n  SHOP\\t")
    raw = "chk\\n2026-03-01\\n-1250\\ncoffee shop"
    assert fingerprint(row) == hashlib.sha256(raw.encode("utf-8")).hexdigest()


def test_hidden_field_boundaries_do_not_collide():
    assert fingerprint(Row("chk1", "2026-03-01", 2, "x")) != fingerprint(Row("chk", "12026-03-01", 2, "x"))
    assert fingerprint(Row("a", "2026-03-01", 12, "3tea")) != fingerprint(Row("a", "2026-03-01", 123, "tea"))


def test_hidden_normalization():
    assert normalize_description("  Corner\\t\\tSTORE \\r\\n #12 ") == "corner store #12"
    assert normalize_description("STRASSE Cafe") == normalize_description("straße café".replace("é", "e"))


def test_hidden_three_identical_two_stored():
    row = Row("chk", "2026-03-01", 450, "Coffee")
    assert classify([row, row, row], [imported(row), imported(row)]) == ["duplicate", "duplicate", "new"]


def test_hidden_user_edited_description_still_duplicate():
    row = Row("chk", "2026-03-04", 6400, "SQ *FARMERS MKT 0041")
    assert classify([row], [imported(row, description="Farmers market veg")]) == ["duplicate"]


def test_hidden_manual_entries_never_match():
    row = Row("chk", "2026-03-05", 2000, "Cash withdrawal")
    manual = StoredRow(row.account, row.date, row.amount_cents, row.description, "manual", None)
    assert classify([row], [manual]) == ["new"]


def test_hidden_bank_changed_description_is_ambiguous():
    old = Row("chk", "2026-03-06", 1899, "AMZN Mktp US*2K4")
    new = Row("chk", "2026-03-06", 1899, "Amazon Marketplace")
    assert classify([new], [imported(old)]) == ["ambiguous"]


def test_hidden_ambiguity_needs_the_same_account():
    old = Row("chk", "2026-03-06", 1899, "AMZN Mktp US*2K4")
    new = Row("sav", "2026-03-06", 1899, "Amazon Marketplace")
    assert classify([new], [imported(old)]) == ["new"]


def test_hidden_exact_matches_win_before_ambiguity():
    exact = Row("chk", "2026-03-07", 999, "Bookshop")
    variant = Row("chk", "2026-03-07", 999, "BOOKSHOP LTD")
    assert classify([variant, exact], [imported(exact)]) == ["new", "duplicate"]


def test_hidden_ambiguous_uses_up_the_stored_row():
    old = Row("chk", "2026-03-08", 500, "Parking")
    a = Row("chk", "2026-03-08", 500, "City Parking 1")
    b = Row("chk", "2026-03-08", 500, "City Parking 2")
    assert classify([a, b], [imported(old)]) == ["ambiguous", "new"]


def test_hidden_mixed_file():
    coffee = Row("chk", "2026-03-01", 450, "Coffee")
    rent = Row("chk", "2026-03-01", 150000, "Rent")
    stored = [imported(coffee), StoredRow("chk", "2026-03-01", 450, "Coffee", "manual", None),
              imported(Row("chk", "2026-03-01", 150000, "RENT MARCH"))]
    incoming = [coffee, rent, coffee, Row("chk", "2026-03-02", 450, "Coffee")]
    assert classify(incoming, stored) == ["duplicate", "ambiguous", "new", "new"]
'''

FP_SOLUTION = FP_DRAFT[:FP_DRAFT.index("def normalize_description")] + '''def normalize_description(text: str) -> str:
    return " ".join(text.split()).casefold()


def fingerprint(row: Row) -> str:
    raw = "\\n".join([row.account, row.date, str(row.amount_cents), normalize_description(row.description)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def classify(incoming: list[Row], stored: list[StoredRow]) -> list[str]:
    candidates = [s for s in stored if s.source == "import"]
    used = [False] * len(candidates)
    labels: list[str | None] = [None] * len(incoming)

    def take(i: int, label: str, matches) -> None:
        for j, s in enumerate(candidates):
            if not used[j] and matches(s):
                used[j], labels[i] = True, label
                return

    for i, row in enumerate(incoming):
        fp = fingerprint(row)
        take(i, "duplicate", lambda s: s.fingerprint == fp)
    for i, row in enumerate(incoming):
        if labels[i] is None:
            take(i, "ambiguous", lambda s: (s.account, s.date, s.amount_cents) == (row.account, row.date,
                                                                                   row.amount_cents))
    return [label or "new" for label in labels]
'''


def fp_check(ctx: Context) -> tuple[bool, str]:
    return _visible_then_hidden(ctx, "tests/test_fingerprint.py", "tests/_hidden_test_fingerprint.py", FP_HIDDEN)


def fp_solve(ctx: Context) -> str:
    write(ctx, "finance/fingerprint.py", FP_SOLUTION)
    return "fixed matching"


# --- 5. ordered_events ------------------------------------------------------
# A consumer that applies whatever arrives, deduplicating only by an in-memory id set.

EVENTS_DRAFT = '''class GapTooLarge(Exception):
    """An event would need more than max_buffer events buffered for its claim."""


class ClaimConsumer:
    """Consumes claim status events from a queue that delivers at least once, in any order.

    An event is {"id": str, "claim": str, "seq": int, "status": str}. Each claim numbers its events 1, 2, 3, ...
    `store` is the durable state, a plain dict the caller persists as JSON between restarts. It holds:
        store["status"][claim]    the claim's current status
        store["last_seq"][claim]  the seq of the last event applied for the claim (absent means 0)
        store["applied"]          the ids of applied events, in the order they were applied
    Anything else (the buffer) lives in memory only and is lost on restart.

    handle(event) returns the ids of the events this call applied, in the order applied.
    - Exactly once, in order: an event is applied when its seq is last_seq + 1 for its claim. Applying sets the
      claim's status and last_seq and appends the event id to store["applied"].
    - After an event is applied, buffered events for the same claim that are now next in sequence are applied
      too, in seq order.
    - Duplicates are ignored (return []): an event whose seq is <= last_seq for its claim, or whose seq is already
      buffered for its claim. The queue redelivers, so a duplicate may carry a different id.
    - Gaps: an event with seq > last_seq + 1 is buffered until the missing events arrive. At most `max_buffer`
      events are buffered per claim. An event that would make one too many raises GapTooLarge and is not
      buffered; nothing else changes, so the queue can redeliver it later.
    - Claims are independent: a gap in one claim never holds up another.
    - Restart: a new ClaimConsumer on the same store carries on where the old one stopped. Events that were
      buffered but never applied are gone, and the queue redelivers them.
    - A seq below 1 raises ValueError.
    """

    def __init__(self, store: dict, max_buffer: int = 5):
        self.store = store
        store.setdefault("status", {})
        store.setdefault("applied", [])
        self.max_buffer = max_buffer
        self._seen: set[str] = set()

    def handle(self, event: dict) -> list[str]:
        if event["id"] in self._seen:
            return []
        self._seen.add(event["id"])
        self.store["status"][event["claim"]] = event["status"]
        self.store["applied"].append(event["id"])
        return [event["id"]]
'''

EVENTS_TESTS = '''from claims.consumer import ClaimConsumer


def ev(claim, seq, status, eid=None):
    return {"id": eid or f"{claim}-{seq}", "claim": claim, "seq": seq, "status": status}


def test_in_order():
    store = {}
    c = ClaimConsumer(store)
    assert c.handle(ev("c1", 1, "received")) == ["c1-1"]
    assert c.handle(ev("c1", 2, "approved")) == ["c1-2"]
    assert store["status"]["c1"] == "approved"


def test_same_event_twice():
    store = {}
    c = ClaimConsumer(store)
    c.handle(ev("c1", 1, "received"))
    assert c.handle(ev("c1", 1, "received")) == []
    assert store["applied"] == ["c1-1"]


def test_late_event_waits_for_the_gap():
    store = {}
    c = ClaimConsumer(store)
    assert c.handle(ev("c1", 2, "approved")) == []
    assert c.handle(ev("c1", 1, "received")) == ["c1-1", "c1-2"]
    assert store["status"]["c1"] == "approved"
'''

EVENTS_HIDDEN = '''import json

import pytest

from claims.consumer import ClaimConsumer, GapTooLarge


def ev(claim, seq, status=None, eid=None):
    return {"id": eid or f"{claim}-{seq}", "claim": claim, "seq": seq, "status": status or f"s{seq}"}


def restart(store, **kw):
    return ClaimConsumer(json.loads(json.dumps(store)), **kw)


def test_hidden_reverse_order():
    store = {}
    c = ClaimConsumer(store)
    assert [c.handle(ev("c", n)) for n in (4, 3, 2)] == [[], [], []]
    assert c.handle(ev("c", 1)) == ["c-1", "c-2", "c-3", "c-4"]
    assert store["last_seq"]["c"] == 4 and store["status"]["c"] == "s4"


def test_hidden_redelivery_with_a_new_id():
    store = {}
    c = ClaimConsumer(store)
    c.handle(ev("c", 1))
    assert c.handle(ev("c", 1, eid="retry-of-c-1")) == []
    assert c.handle(ev("c", 3)) == []
    assert c.handle(ev("c", 3, eid="retry-of-c-3")) == []
    assert c.handle(ev("c", 2)) == ["c-2", "c-3"]
    assert store["applied"] == ["c-1", "c-2", "c-3"]


def test_hidden_stale_event_does_not_roll_back():
    store = {}
    c = ClaimConsumer(store)
    for n in (1, 2, 3):
        c.handle(ev("c", n))
    assert c.handle(ev("c", 2, "s2", eid="late")) == []
    assert store["status"]["c"] == "s3"


def test_hidden_buffer_limit():
    store = {}
    c = ClaimConsumer(store, max_buffer=2)
    c.handle(ev("c", 3))
    c.handle(ev("c", 4))
    with pytest.raises(GapTooLarge):
        c.handle(ev("c", 5))
    assert c.handle(ev("c", 4, eid="dup")) == []
    assert c.handle(ev("c", 1)) == ["c-1"]
    assert c.handle(ev("c", 2)) == ["c-2", "c-3", "c-4"]
    assert c.handle(ev("c", 5)) == ["c-5"]


def test_hidden_buffer_limit_is_per_claim():
    store = {}
    c = ClaimConsumer(store, max_buffer=1)
    c.handle(ev("a", 2))
    with pytest.raises(GapTooLarge):
        c.handle(ev("a", 3))
    assert c.handle(ev("b", 2)) == []
    assert c.handle(ev("b", 1)) == ["b-1", "b-2"]
    assert c.handle(ev("a", 1)) == ["a-1", "a-2"]
    assert store["applied"] == ["b-1", "b-2", "a-1", "a-2"]


def test_hidden_claims_are_independent():
    store = {}
    c = ClaimConsumer(store)
    assert c.handle(ev("a", 2)) == []
    assert c.handle(ev("b", 1)) == ["b-1"]
    assert "a" not in store["status"]


def test_hidden_restart_and_full_replay():
    log = [ev("c", n) for n in range(1, 6)] + [ev("d", n) for n in range(1, 4)]
    store = {}
    c = ClaimConsumer(store)
    for e in (log[0], log[2], log[5], log[3]):
        c.handle(e)
    c = restart(store)
    applied = [i for e in log for i in c.handle(e)]
    assert applied == ["c-2", "c-3", "c-4", "c-5", "d-2", "d-3"]
    assert c.store["applied"] == ["c-1", "d-1", "c-2", "c-3", "c-4", "c-5", "d-2", "d-3"]
    assert c.store["status"] == {"c": "s5", "d": "s3"}


def test_hidden_restart_forgets_the_buffer_not_the_progress():
    store = {}
    c = ClaimConsumer(store)
    c.handle(ev("c", 1))
    c.handle(ev("c", 3))
    c = restart(store)
    assert c.handle(ev("c", 1, eid="redelivered")) == []
    assert c.handle(ev("c", 2)) == ["c-2"]
    assert c.handle(ev("c", 3)) == ["c-3"]


def test_hidden_bad_seq():
    with pytest.raises(ValueError):
        ClaimConsumer({}).handle(ev("c", 0))
'''

EVENTS_SOLUTION = EVENTS_DRAFT[:EVENTS_DRAFT.index("    def __init__")] + '''    def __init__(self, store: dict, max_buffer: int = 5):
        self.store = store
        store.setdefault("status", {})
        store.setdefault("last_seq", {})
        store.setdefault("applied", [])
        self.max_buffer = max_buffer
        self._buffer: dict[str, dict[int, dict]] = {}

    def handle(self, event: dict) -> list[str]:
        claim, seq = event["claim"], event["seq"]
        if seq < 1:
            raise ValueError(f"seq must be >= 1, got {seq}")
        last = self.store["last_seq"].get(claim, 0)
        pending = self._buffer.setdefault(claim, {})
        if seq <= last or seq in pending:
            return []
        if seq > last + 1:
            if len(pending) >= self.max_buffer:
                raise GapTooLarge(f"claim {claim}: waiting for seq {last + 1}, {len(pending)} already buffered")
            pending[seq] = event
            return []
        applied = []
        while event is not None:
            self._apply(event)
            applied.append(event["id"])
            event = pending.pop(event["seq"] + 1, None)
        return applied

    def _apply(self, event: dict) -> None:
        self.store["status"][event["claim"]] = event["status"]
        self.store["last_seq"][event["claim"]] = event["seq"]
        self.store["applied"].append(event["id"])
'''


def events_check(ctx: Context) -> tuple[bool, str]:
    return _visible_then_hidden(ctx, "tests/test_consumer.py", "tests/_hidden_test_consumer.py", EVENTS_HIDDEN)


def events_solve(ctx: Context) -> str:
    write(ctx, "claims/consumer.py", EVENTS_SOLUTION)
    return "rewrote the consumer"


# --- 6. bank_csv_shapes -----------------------------------------------------
# A parser that knows one bank's shape and converts through float.

CSV_DOC = '''"""Parse a bank's CSV export into transactions. The header row decides the shape.

    Header names match case-insensitively after trimming whitespace, in the order listed.

    1. Signed: `Date,Description,Amount`. Date `MM/DD/YYYY`. `-12.34` is money out, `12.34` money in.
    2. Debit/credit: `Posting Date,Payee,Debit,Credit`. Date `YYYY-MM-DD`. Exactly one of Debit and Credit is filled
       in each row: Debit is money out (negative), Credit money in. Both filled or both empty raises ValueError.
    3. Parenthesised: `Transaction Date,Memo,Amount (USD)`. Date `DD Mon YYYY` (`05 Mar 2026`, English month
       abbreviations). Money out is in parentheses: `(12.34)`. A `$` may come first or sit inside the parentheses:
       `$1,234.56`, `($1,234.56)` and `$(1,234.56)` are all valid.

    Every shape:
    - Amounts may use thousands separators (`1,234.56`, quoted in the CSV) and have at most two decimal places
      (`12`, `12.5`, `12.50`). Convert exactly to integer cents, never through float. More than two decimal places,
      or anything else that isn't a number, raises ValueError.
    - The file may start with a UTF-8 byte-order mark (U+FEFF) and may use CRLF line endings.
    - Blank rows (empty lines, or rows whose cells are all empty or whitespace) are skipped wherever they are;
      exports often end with a few `,,,` rows.
    - Descriptions are trimmed of surrounding whitespace.
    - Any other header, or a row with the wrong number of cells, raises ValueError.
    - Transactions come back in file order.
    """'''

CSV_DRAFT = '''import csv
import io
from dataclasses import dataclass
from datetime import date, datetime


@dataclass
class Txn:
    date: date
    description: str
    amount_cents: int


def parse_csv(text: str) -> list[Txn]:
    ''' + CSV_DOC + '''
    rows = list(csv.reader(io.StringIO(text)))
    header = rows[0]
    if header != ["Date", "Description", "Amount"]:
        raise ValueError(f"unknown CSV shape: {header}")
    out = []
    for row in rows[1:]:
        when = datetime.strptime(row[0], "%m/%d/%Y").date()
        out.append(Txn(when, row[1], int(float(row[2]) * 100)))
    return out
'''

CSV_TESTS = '''from datetime import date

from bankcsv.parser import Txn, parse_csv


def test_signed_shape():
    text = "Date,Description,Amount\\n03/05/2026,Grocer,-45.10\\n03/06/2026,Payroll,2000.00\\n"
    assert parse_csv(text) == [Txn(date(2026, 3, 5), "Grocer", -4510), Txn(date(2026, 3, 6), "Payroll", 200000)]


def test_debit_credit_shape():
    text = "Posting Date,Payee,Debit,Credit\\n2026-03-05,Grocer,45.10,\\n2026-03-06,Payroll,,2000.00\\n"
    assert parse_csv(text) == [Txn(date(2026, 3, 5), "Grocer", -4510), Txn(date(2026, 3, 6), "Payroll", 200000)]


def test_parenthesised_shape():
    text = "Transaction Date,Memo,Amount (USD)\\n05 Mar 2026,Grocer,(45.10)\\n06 Mar 2026,Payroll,2000.00\\n"
    assert parse_csv(text) == [Txn(date(2026, 3, 5), "Grocer", -4510), Txn(date(2026, 3, 6), "Payroll", 200000)]
'''

CSV_HIDDEN = '''from datetime import date

import pytest

from bankcsv.parser import Txn, parse_csv


def amounts(text):
    return [t.amount_cents for t in parse_csv(text)]


def test_hidden_no_float_rounding():
    text = "Date,Description,Amount\\n01/02/2026,a,-0.29\\n01/02/2026,b,1.15\\n01/02/2026,c,-1234.5\\n01/02/2026,d,7\\n"
    assert amounts(text) == [-29, 115, -123450, 700]


def test_hidden_thousands_separators():
    text = 'Date,Description,Amount\\n01/02/2026,House,"-12,345.67"\\n01/03/2026,Bonus,"1,000"\\n'
    assert amounts(text) == [-1234567, 100000]


def test_hidden_bom_crlf_and_trailing_blank_rows():
    text = "\\ufeffPosting Date,Payee,Debit,Credit\\r\\n2026-02-28,Rent,\\"1,500.00\\",\\r\\n\\r\\n2026-03-01,Refund,,12.5\\r\\n,,,\\r\\n , , ,\\r\\n"
    assert parse_csv(text) == [Txn(date(2026, 2, 28), "Rent", -150000), Txn(date(2026, 3, 1), "Refund", 1250)]


def test_hidden_header_case_and_spacing():
    text = " posting date , PAYEE,debit , Credit\\n2026-03-05,  Grocer  ,1.00,\\n"
    assert parse_csv(text) == [Txn(date(2026, 3, 5), "Grocer", -100)]


@pytest.mark.parametrize("cell,cents", [
    ("(45.10)", -4510), ('"$1,234.56"', 123456), ('"($1,234.56)"', -123456), ('"$(1,234.56)"', -123456),
    ("0.05", 5), ("(0.5)", -50),
])
def test_hidden_parenthesised_amounts(cell, cents):
    text = f"Transaction Date,Memo,Amount (USD)\\n9 Jan 2026,x,{cell}\\n"
    assert amounts(text) == [cents]


def test_hidden_parenthesised_dates():
    text = "Transaction Date,Memo,Amount (USD)\\n31 Dec 2025,a,1\\n01 Feb 2026,b,2\\n"
    assert [t.date for t in parse_csv(text)] == [date(2025, 12, 31), date(2026, 2, 1)]


@pytest.mark.parametrize("text", [
    "Date,Description,Amount\\n01/02/2026,a,1.234\\n",
    "Date,Description,Amount\\n01/02/2026,a,abc\\n",
    "Date,Description,Amount\\n01/02/2026,a,1.2.3\\n",
    "Posting Date,Payee,Debit,Credit\\n2026-03-05,x,1.00,2.00\\n",
    "Posting Date,Payee,Debit,Credit\\n2026-03-05,x,,\\n",
    "Date,Payee,Amount\\n01/02/2026,a,1\\n",
    "Date,Description,Amount\\n01/02/2026,a\\n",
])
def test_hidden_rejects(text):
    with pytest.raises(ValueError):
        parse_csv(text)


def test_hidden_quoted_commas_in_descriptions():
    text = 'Date,Description,Amount\\n04/01/2026,"Smith, J - rent split",-600.00\\n'
    assert parse_csv(text) == [Txn(date(2026, 4, 1), "Smith, J - rent split", -60000)]
'''

CSV_SOLUTION = '''import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime


@dataclass
class Txn:
    date: date
    description: str
    amount_cents: int


_NUMBER = re.compile(r"(\\d{1,3}(?:,\\d{3})+|\\d+)(?:\\.(\\d{1,2}))?")


def _cents(text: str) -> int:
    match = _NUMBER.fullmatch(text)
    if not match:
        raise ValueError(f"bad amount: {text!r}")
    return int(match.group(1).replace(",", "")) * 100 + int((match.group(2) or "0").ljust(2, "0"))


def _signed(cell: str) -> int:
    s = cell.strip()
    negative = s.startswith("-")
    s = s.removeprefix("-").removeprefix("$")
    return -_cents(s) if negative else _cents(s)


def _parenthesised(cell: str) -> int:
    s = cell.strip().removeprefix("$")
    negative = s.startswith("(") and s.endswith(")")
    if negative:
        s = s[1:-1]
    s = s.removeprefix("$")
    return -_cents(s) if negative else _cents(s)


def _debit_credit(debit: str, credit: str) -> int:
    debit, credit = debit.strip(), credit.strip()
    if bool(debit) == bool(credit):
        raise ValueError("exactly one of debit and credit must be filled")
    return -_cents(debit) if debit else _cents(credit)


SHAPES = {
    ("date", "description", "amount"):
        lambda r: (datetime.strptime(r[0].strip(), "%m/%d/%Y").date(), _signed(r[2])),
    ("posting date", "payee", "debit", "credit"):
        lambda r: (date.fromisoformat(r[0].strip()), _debit_credit(r[2], r[3])),
    ("transaction date", "memo", "amount (usd)"):
        lambda r: (datetime.strptime(r[0].strip(), "%d %b %Y").date(), _parenthesised(r[2])),
}


def parse_csv(text: str) -> list[Txn]:
    rows = [r for r in csv.reader(io.StringIO(text.removeprefix("\\ufeff"))) if any(c.strip() for c in r)]
    if not rows:
        raise ValueError("empty CSV")
    header = tuple(c.strip().lower() for c in rows[0])
    if header not in SHAPES:
        raise ValueError(f"unknown CSV shape: {rows[0]}")
    convert = SHAPES[header]
    out = []
    for row in rows[1:]:
        if len(row) != len(header):
            raise ValueError(f"expected {len(header)} cells, got {len(row)}: {row}")
        when, cents = convert(row)
        out.append(Txn(when, row[1].strip(), cents))
    return out
'''


def csv_check(ctx: Context) -> tuple[bool, str]:
    return _visible_then_hidden(ctx, "tests/test_parser.py", "tests/_hidden_test_parser.py", CSV_HIDDEN)


def csv_solve(ctx: Context) -> str:
    write(ctx, "bankcsv/parser.py", CSV_SOLUTION)
    return "handled all three shapes"


# --- registry ---------------------------------------------------------------

_KEEP_TESTS = "Do not modify the existing tests; add tests for the rules you implement and run them."

HELDOUT_TASKS: list[Task] = [
    Task("cali_draw", "code",
         "league/draw.py is a first draft of the doubles draw for league night and doesn't follow the rules in its "
         "docstring. Make `draw` follow every rule there exactly, including the shuffle procedure, so a given seed "
         f"always produces the same draw. {_KEEP_TESTS}",
         lambda: {"league/__init__.py": "", "league/draw.py": DRAW_DRAFT, "tests/test_draw.py": DRAW_TESTS},
         draw_check, draw_solve),
    Task("ace_pot_ledger", "debug",
         "league/ledger.py keeps the league-night money: payouts, closest-to-the-pin and the ace pot. The treasurer "
         "says the numbers don't add up. Make `Ledger` follow its docstring exactly, including rounding, the pot "
         f"cap and the season reset. {_KEEP_TESTS}",
         lambda: {"league/__init__.py": "", "league/ledger.py": LEDGER_DRAFT, "tests/test_ledger.py": LEDGER_TESTS},
         ledger_check, ledger_solve),
    Task("recurring_charges", "code",
         "Implement `recurring_alerts` in finance/recurring.py exactly as the module docstring describes, including "
         "month-end billing days and every boundary. Write tests covering the rules and run them.",
         lambda: {"finance/__init__.py": "", "finance/recurring.py": RECUR_STUB, "tests/__init__.py": ""},
         recur_check, recur_solve),
    Task("reimport_overlap", "debug",
         "finance/fingerprint.py decides which rows of a re-imported bank CSV are already stored. Users report both "
         "false duplicates and missed duplicates. Make it follow the module docstring exactly. " + _KEEP_TESTS,
         lambda: {"finance/__init__.py": "", "finance/fingerprint.py": FP_DRAFT,
                  "tests/test_fingerprint.py": FP_TESTS},
         fp_check, fp_solve),
    Task("ordered_events", "code",
         "claims/consumer.py applies claim status events from an at-least-once queue, but it applies them in "
         "arrival order and forgets what it has seen when it restarts. Make `ClaimConsumer` follow its docstring: "
         f"exactly once, in sequence order, with bounded buffering, and correct after a restart. {_KEEP_TESTS}",
         lambda: {"claims/__init__.py": "", "claims/consumer.py": EVENTS_DRAFT, "tests/test_consumer.py": EVENTS_TESTS},
         events_check, events_solve),
    Task("bank_csv_shapes", "code",
         "bankcsv/parser.py only understands one bank's CSV export. Make `parse_csv` handle all three shapes in its "
         f"docstring and every rule listed there. {_KEEP_TESTS}",
         lambda: {"bankcsv/__init__.py": "", "bankcsv/parser.py": CSV_DRAFT, "tests/test_parser.py": CSV_TESTS},
         csv_check, csv_solve),
]

for _task in HELDOUT_TASKS:
    _task.max_turns, _task.wall_limit = HARD_LIMITS["max_turns"], HARD_LIMITS["wall_limit"]
