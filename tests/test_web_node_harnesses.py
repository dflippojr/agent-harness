"""Pure Stables Node harnesses (stables/tests) retain isolated pytest cases and launch behavior."""
import shutil
import subprocess
from pathlib import Path

import pytest

STABLES_TESTS = Path(__file__).resolve().parents[1] / "stables" / "tests"


@pytest.mark.parametrize(
    "script_name, timeout, marker",
    [
        # First-document splash readiness, fallback, and motion policy (#179).
        pytest.param("web_boot_splash.mjs", None, "ok", id="test_boot_splash_lifecycle"),
        # Session-page mask / summary / elide compaction notes (#248).
        pytest.param("web_compaction.mjs", None, "ok", id="test_session_compaction_events_render_mask_without_touching_summary"),
        # Jobs split, form actions, attention groups and inline Enabled switch (#511, #567).
        pytest.param("web_jobs.mjs", 30, "ok", id="test_jobs_list_groups_and_inline_switch"),
        # The Web lib modules run under plain Node with a stub DOM (#372).
        pytest.param(
            "web_lib_modules.mjs", None,
            "ok: lib modules import under plain Node and behave on their own",
            id="test_lib_modules",
        ),
        # md() output is unchanged by the Sonar refactor (#206).
        pytest.param("web_markdown.mjs", None, "ok", id="test_markdown_renders_recorded_html"),
        # md() links stay linear on crafted input and match legitimate input (S8786, #239).
        pytest.param("web_markdown_links.mjs", None, "ok", id="test_markdown_links_are_linear_and_unchanged"),
        # New task follows GPU hold: Claude default, Backend notice, Queue task (#185).
        pytest.param("web_new_task_gpu_hold.mjs", None, "ok", id="test_new_task_gpu_hold_default_notice_and_queue_button"),
        # Pure web helpers extracted by the Sonar style cleanup keep their output (#229).
        pytest.param("web_pure_helpers.mjs", None, "ok", id="test_pure_helpers_keep_their_output"),
        # Diagnostics read once and never poll (#311); Images names an upscale or edit in flight and its choices.
        pytest.param("web_resources_and_images.mjs", 30, "ok", id="test_diagnostics_do_not_poll_and_image_stages_are_labelled"),
        # Agents list groups sessions and shows the pending ask inline (#509).
        pytest.param("web_session_groups.mjs", None, "ok", id="test_session_groups"),
        # Session jump arrows follow scroll position and page height (#184).
        pytest.param("web_session_jumps.mjs", None, "ok", id="test_session_jump_visibility_from_scroll_and_page_height"),
        # Replaying stored taint_added keeps one entry per source (#262 review).
        pytest.param("web_taint.mjs", None, "ok", id="test_taint_replay_does_not_duplicate"),
    ],
)
def test_web_node_harness(script_name, timeout, marker):
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = STABLES_TESTS / script_name
    result = subprocess.run(
        [node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=timeout,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker in result.stdout
