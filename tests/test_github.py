import email.message
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import github  # noqa: E402


def response(items, link):
    page = io.BytesIO(json.dumps(items).encode())
    page.headers = email.message.Message()
    page.headers["Link"] = link
    return page


class CursorPagesTest(unittest.TestCase):
    def test_cursor_pages_follow_the_next_link_to_the_last_page(self):
        first = "https://api.github.com/repos/org/app/activity?ref=refs/heads/main&per_page=100"
        second = "https://api.github.com/repositories/7/activity?ref=refs%2Fheads%2Fmain&per_page=100&after=Y3Vyc29y"
        pages = {first: response([{"id": 3}, {"id": 2}], f'<{second}>; rel="next"'),
                 second: response([{"id": 1}], f'<{first}>; rel="prev"')}
        requested = []

        def urlopen(request, timeout):
            requested.append(request.full_url)
            return pages[request.full_url]

        with mock.patch.object(github, "urlopen", urlopen):
            items = list(github.GitHub("token").cursor_pages("repos/org/app/activity?ref=refs/heads/main"))
        self.assertEqual(items, [{"id": 3}, {"id": 2}, {"id": 1}])
        self.assertEqual(requested, [first, second])


if __name__ == "__main__":
    unittest.main()
