from demo import github_app as demo_gh
from demo.content import DEMO_PR_NUMBER, DEMO_REPO


def test_fetch_returns_the_canned_diff_as_a_non_draft():
    diff = demo_gh.fetch_pr_diff(DEMO_REPO, DEMO_PR_NUMBER)
    assert diff.repo_full_name == DEMO_REPO
    assert diff.draft is False
    assert "app/auth.py" in diff.text


def test_upsert_creates_then_edits_in_place():
    demo_gh.reset()
    first = demo_gh.upsert_comment(DEMO_REPO, DEMO_PR_NUMBER, "body one")
    again = demo_gh.upsert_comment(DEMO_REPO, DEMO_PR_NUMBER, "body two", comment_id=first.id)
    assert again.id == first.id
    assert again.body == "body two"


def test_installation_id_check_makes_no_network_call():
    assert demo_gh.discover_and_verify_installation_id(0) == 1
