"""Read-only tests for deployment gating and canonical URL ownership."""
import contextlib
import io
import unittest
from pathlib import Path
from notify_indexnow import (
    HOST, PAGES_PATH, canonical_urls, deployed_run, public_files, wait_until_public,
)

REVISION = "a" * 40


def run_record(**changes):
    return {"id": 1, "path": PAGES_PATH, "head_branch": "main", "head_sha": REVISION,
            "status": "completed", "conclusion": "success", **changes}


class FakeClient:
    def __init__(self, runs=None, bodies=None, head=REVISION):
        self.runs = runs or [[run_record()]]
        self.bodies = bodies or [b"new"]
        self.head = head
        self.round = -1
        self.reads = 0

    def api(self, path):
        if path == "/commits/main":
            return {"sha": self.head}
        self.round += 1
        return {"workflow_runs": self.runs[min(self.round, len(self.runs) - 1)]}

    def request(self, url):
        self.reads += 1
        return 200, self.bodies[min(self.round, len(self.bodies) - 1)]


class DeploymentNotificationTests(unittest.TestCase):
    def setUp(self):
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()

    def tearDown(self):
        self.output.__exit__(None, None, None)

    def test_each_repository_owns_only_its_canonical_pages(self):
        repo = Path(__file__).resolve().parents[2]
        sitemap = (repo / "sitemap.xml").read_bytes()
        scope = "root" if repo.name.endswith(".github.io") else "project"
        urls = canonical_urls(sitemap, scope)
        if scope == "root":
            self.assertEqual(urls, [HOST + "/"])
        else:
            self.assertGreater(len(urls), 1)
            self.assertTrue(all(url.startswith(HOST + "/huwa-chongli/") for url in urls))
            self.assertNotIn(HOST + "/", urls)
        files = public_files(repo, scope, urls)
        self.assertTrue(all(files[url] for url in urls))

    def test_noncanonical_host_is_rejected(self):
        sitemap = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://example.com/</loc></url></urlset>'
        with self.assertRaises(ValueError):
            canonical_urls(sitemap, "root")

    def test_only_exact_main_pages_revision_qualifies(self):
        for change in [{"head_sha": "b" * 40}, {"head_branch": "feature"},
                       {"path": ".github/workflows/indexnow.yml"}, {"status": "in_progress"}]:
            self.assertFalse(deployed_run([run_record(**change)], REVISION))
        self.assertTrue(deployed_run([run_record()], REVISION))

    def test_failed_deployment_stops_before_public_check(self):
        client = FakeClient(runs=[[run_record(conclusion="failure")]])
        with self.assertRaises(RuntimeError):
            wait_until_public(client, REVISION, {HOST + "/": b"new"}, 1, 0)
        self.assertEqual(client.reads, 0)

    def test_waits_for_deployment_then_for_new_public_bytes(self):
        client = FakeClient(
            runs=[[run_record(status="in_progress")], [run_record()], [run_record()]],
            bodies=[b"old", b"old", b"new"],
        )
        self.assertTrue(wait_until_public(client, REVISION, {HOST + "/": b"new"}, 3, 0))
        self.assertEqual(client.round, 2)

    def test_stale_live_content_never_passes(self):
        client = FakeClient(bodies=[b"old"])
        with self.assertRaises(RuntimeError):
            wait_until_public(client, REVISION, {HOST + "/": b"new"}, 2, 0)

    def test_superseded_revision_does_not_submit(self):
        client = FakeClient(head="b" * 40)
        self.assertFalse(wait_until_public(client, REVISION, {HOST + "/": b"new"}, 1, 0))
        self.assertEqual(client.reads, 0)

    def test_head_advance_during_content_checks_stops_submission(self):
        client = FakeClient()
        original = client.request

        def advance(url):
            result = original(url)
            client.head = "b" * 40
            return result

        client.request = advance
        self.assertFalse(wait_until_public(client, REVISION, {HOST + "/": b"new"}, 1, 0))


if __name__ == "__main__":
    unittest.main()
