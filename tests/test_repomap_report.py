"""Go/no-go rule for the repo-map study (#264)."""

from bakeoff.repomap_report import arm_stats, decide, reduction


def arm(pass_rate, turns, tokens):
    return {"runs": 10, "pass_rate": pass_rate, "turns": turns, "prompt_tokens": tokens, "wall_seconds": 1.0}


HARD = (arm(0.8, 20, 100), arm(0.8, 20, 100))


def test_go_on_turn_drop():
    assert decide((arm(0.8, 20, 1000), arm(0.8, 16, 1000)), HARD)[0]


def test_go_on_token_drop():
    assert decide((arm(0.8, 20, 1000), arm(0.9, 20, 800)), HARD)[0]


def test_no_go_below_threshold_or_pass_regression():
    assert not decide((arm(0.8, 20, 1000), arm(0.8, 18, 900)), HARD)[0]
    assert not decide((arm(0.8, 20, 1000), arm(0.6, 10, 500)), HARD)[0]
    assert not decide((arm(0.8, 20, 1000), arm(0.8, 10, 500)), (arm(0.8, 20, 100), arm(0.7, 20, 100)))[0]


def test_stats_and_reduction():
    s = arm_stats([{"passed": True, "turns": 4, "prompt_tokens": 100, "wall_seconds": 2},
                   {"passed": False, "turns": 6, "prompt_tokens": 300, "wall_seconds": 4}])
    assert s["pass_rate"] == 0.5 and s["turns"] == 5 and s["prompt_tokens"] == 200
    assert reduction(10, 8) == 0.2 and reduction(0, 5) == 0.0
