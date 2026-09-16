"""The fixed content the demo reviews, and the findings it always reports.

Deterministic on purpose: the same diff and the same findings every time, so
the demo cannot embarrass itself and the article's screenshots match what a
reader sees.
"""

from __future__ import annotations

DEMO_REPO = "bot-demo/example-app"
DEMO_PR_NUMBER = 7
DEMO_PR_TITLE = "Add password reset flow"
DEMO_HEAD_SHA = "d3m0d3m0d3m0d3m0d3m0d3m0d3m0d3m0d3m0d3m0"

DEMO_DIFF = '''diff --git a/app/auth.py b/app/auth.py
index 1a2b3c4..5d6e7f8 100644
--- a/app/auth.py
+++ b/app/auth.py
@@ -84,6 +84,9 @@ def reset_password(token: str, new_password: str) -> bool:
     user = lookup_reset_token(token)
     if user is None:
         return False
+    if not verify(token):
+        logger.warning("reset failed for api_key=%s", settings.API_KEY)
+        return False
     user.set_password(new_password)
     return True
diff --git a/app/api/users.py b/app/api/users.py
index 2b3c4d5..6e7f8a9 100644
--- a/app/api/users.py
+++ b/app/api/users.py
@@ -142,6 +142,8 @@ def list_team_members(team_id: int) -> list[dict]:
     members = []
     for membership in Membership.objects.filter(team_id=team_id):
+        profile = Profile.objects.get(user_id=membership.user_id)
+        members.append({"name": profile.name, "email": profile.email})
     return members
diff --git a/app/utils/format.py b/app/utils/format.py
index 3c4d5e6..7f8a9b0 100644
--- a/app/utils/format.py
+++ b/app/utils/format.py
@@ -20,6 +20,8 @@ def format_created_at(value):
 -    return value.strftime("%Y-%m-%d")
 +    return value.strftime("%Y-%m-%d")
 +
 +def format_updated_at(value):
 +    return value.strftime("%Y-%m-%d")
 '''

# Keyed by container-schema class name, because MockProvider is handed the
# container schema and must answer with findings of the matching shape.
FINDINGS_BY_SCHEMA: dict[str, list[dict]] = {
    "SecurityFindings": [
        {
            "severity": "critical",
            "file": "app/auth.py",
            "line": 88,
            "description": (
                "The API key is written to the log in plaintext when a password "
                "reset fails, so every failed reset leaks a live credential into "
                "log storage."
            ),
            "fix": (
                "Log only the key's length or a truncated hash, never the value "
                "itself; rotate the key if these logs have already been shipped."
            ),
        }
    ],
    "PerformanceFindings": [
        {
            "type": "N+1",
            "estimated_impact": "high",
            "file": "app/api/users.py",
            "line": 145,
            "suggestion": (
                "Each loop iteration issues its own profile query. Fetch all "
                "profiles in one call with select_related or a single "
                "filter(user_id__in=...) lookup keyed by user id."
            ),
        }
    ],
    "QualityFindings": [
        {
            "category": "duplication",
            "file": "app/utils/format.py",
            "line": 22,
            "issue": (
                "format_updated_at is byte-for-byte identical to "
                "format_created_at, so the date format now has two homes."
            ),
            "refactoring_suggestion": (
                "Delete one and expose a single format_date(value) that both "
                "call sites use."
            ),
        }
    ],
}
