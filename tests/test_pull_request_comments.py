import inspect
import unittest
from unittest.mock import AsyncMock, patch

from mcp.server.mcpserver.exceptions import ToolError

import bitbucket_mcp


def comment(comment_id: int, text: str, **values: object) -> dict[str, object]:
    return {
        "id": comment_id,
        "version": values.pop("version", 1),
        "text": text,
        "author": {"slug": "reviewer", "displayName": "Reviewer"},
        "createdDate": 100,
        "updatedDate": 100,
        "comments": [],
        **values,
    }


def activity(
    activity_id: int,
    created_at: int,
    value: dict[str, object],
    action: str = "ADDED",
    **values: object,
) -> dict[str, object]:
    return {
        "id": activity_id,
        "createdDate": created_at,
        "action": "COMMENTED",
        "commentAction": action,
        "comment": value,
        **values,
    }


class PullRequestCommentNormalizationTest(unittest.TestCase):
    def test_ignores_non_comment_activities(self) -> None:
        activities = [
            {"id": 1, "createdDate": 1, "action": "APPROVED"},
            activity(2, 2, comment(20, "general")),
            {"id": 3, "createdDate": 3, "action": "RESCOPED"},
            {"id": 4, "createdDate": 4, "action": "MERGED"},
        ]

        self.assertEqual(
            [item["id"] for item in bitbucket_mcp._current_comments(activities)],
            [20],
        )

    def test_normalizes_general_and_inline_anchors(self) -> None:
        activities = [
            activity(1, 1, comment(2, "general")),
            activity(
                2,
                2,
                comment(1, "inline"),
                commentAnchor={
                    "path": {"toString": "src/new.py"},
                    "srcPath": {"components": ["src", "old.py"]},
                    "line": 12,
                    "lineType": "ADDED",
                    "fileType": "TO",
                    "diffType": "EFFECTIVE",
                    "orphaned": True,
                },
            ),
            activity(
                3,
                3,
                comment(
                    3,
                    "embedded anchor",
                    anchor={
                        "path": "src/embedded.py",
                        "line": 7,
                        "lineType": "CONTEXT",
                        "fileType": "FROM",
                        "diffType": "EFFECTIVE",
                    },
                ),
            ),
        ]

        comments = bitbucket_mcp._current_comments(activities)

        self.assertNotIn("anchor", comments[1])
        self.assertEqual(
            comments[0]["anchor"],
            {
                "path": "src/new.py",
                "source_path": "src/old.py",
                "line": 12,
                "line_type": "ADDED",
                "file_type": "TO",
                "diff_type": "EFFECTIVE",
            },
        )
        self.assertEqual(comments[2]["anchor"]["path"], "src/embedded.py")

    def test_returns_roots_and_nested_replies(self) -> None:
        root = comment(
            10,
            "root",
            comments=[
                comment(
                    12,
                    "second reply",
                    comments=[comment(13, "nested reply")],
                ),
                comment(11, "first reply"),
            ],
        )

        comments = bitbucket_mcp._current_comments([activity(1, 1, root)])

        self.assertEqual([item["id"] for item in comments], [10])
        self.assertEqual([item["id"] for item in comments[0]["replies"]], [11, 12])
        self.assertEqual(comments[0]["replies"][1]["replies"][0]["id"], 13)

    def test_collapses_edits_deletes_and_resolution_state(self) -> None:
        activities = [
            activity(1, 10, comment(1, "old")),
            activity(
                2,
                20,
                comment(1, "edited", version=2, threadResolved=True),
                "UPDATED",
            ),
            activity(3, 11, comment(2, "deleted")),
            activity(4, 30, {"id": 2}, "DELETED"),
            activity(
                5,
                15,
                comment(
                    3,
                    "orphaned",
                    anchor={"path": "old.py", "line": 4, "orphaned": True},
                ),
            ),
        ]

        comments = bitbucket_mcp._current_comments(activities)

        self.assertEqual([item["id"] for item in comments], [1, 3])
        self.assertEqual(comments[0]["text"], "edited")
        self.assertTrue(comments[0]["resolved"])
        self.assertEqual(comments[1]["anchor"]["path"], "old.py")

    def test_rejects_malformed_comment_activity(self) -> None:
        with self.assertRaisesRegex(ToolError, "valid ID"):
            bitbucket_mcp._current_comments([activity(1, 1, {"text": "missing ID"})])
        with self.assertRaisesRegex(ToolError, "without a comment"):
            bitbucket_mcp._current_comments(
                [
                    {
                        "id": 1,
                        "createdDate": 1,
                        "action": "COMMENTED",
                        "commentAction": "ADDED",
                    }
                ]
            )


class PullRequestCommentsToolTest(unittest.IsolatedAsyncioTestCase):
    async def test_fetches_all_activity_pages_then_paginates_comments(self) -> None:
        pages = [
            {
                "values": [activity(5, 5, comment(5, "five"))],
                "isLastPage": False,
                "nextPageStart": 40,
            },
            {
                "values": [
                    activity(1, 1, comment(1, "one")),
                    activity(3, 3, comment(3, "three")),
                ],
                "isLastPage": True,
            },
        ]
        request = AsyncMock(side_effect=pages)
        with patch.object(bitbucket_mcp, "_request_json", request):
            result = await bitbucket_mcp.get_pull_request_comments(
                "PRJ", "repo", 146, cursor=1, limit=1
            )

        self.assertEqual([item["id"] for item in result["comments"]], [3])
        self.assertEqual(result["next_cursor"], 2)
        self.assertEqual(request.await_count, 2)
        self.assertEqual(request.await_args_list[0].kwargs["params"]["start"], 0)
        self.assertEqual(request.await_args_list[1].kwargs["params"]["start"], 40)
        self.assertTrue(
            request.await_args_list[0].args[1].endswith("/pull-requests/146/activities")
        )

    async def test_rejects_stalled_activity_pagination(self) -> None:
        page = {
            "values": [],
            "isLastPage": False,
            "nextPageStart": 0,
        }
        with (
            patch.object(bitbucket_mcp, "_request_json", AsyncMock(return_value=page)),
            self.assertRaisesRegex(ToolError, "did not make progress"),
        ):
            await bitbucket_mcp.get_pull_request_comments("PRJ", "repo", 146)

    async def test_rejects_incomplete_activity_pagination(self) -> None:
        page = {
            "values": [],
            "isLastPage": False,
            "nextPageStart": 1,
        }
        with (
            patch.object(bitbucket_mcp, "MAX_PAGES", 1),
            patch.object(bitbucket_mcp, "_request_json", AsyncMock(return_value=page)),
            self.assertRaisesRegex(ToolError, "exceeded the safe page limit"),
        ):
            await bitbucket_mcp.get_pull_request_comments("PRJ", "repo", 146)

    def test_public_input_schema_is_unchanged(self) -> None:
        signature = inspect.signature(bitbucket_mcp.get_pull_request_comments)

        self.assertEqual(
            list(signature.parameters),
            ["project", "repo", "pr_id", "cursor", "limit"],
        )
        self.assertEqual(signature.parameters["cursor"].default, 0)
        self.assertEqual(signature.parameters["limit"].default, 25)


if __name__ == "__main__":
    unittest.main()
