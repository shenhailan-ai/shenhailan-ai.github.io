"""Submit canonical URLs only after this main revision is deployed and public."""
import argparse
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HOST = "https://huwachongli.com"
PREFIX = "/huwa-chongli/"
PAGES_PATH = "dynamic/pages/pages-build-deployment"
REPOSITORIES = {
    "shenhailan-ai/shenhailan-ai.github.io": "root",
    "shenhailan-ai/huwa-chongli": "project",
}


def canonical_urls(sitemap, scope):
    root = ET.fromstring(sitemap)
    namespace = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls = []
    for node in root.findall("s:url/s:loc", namespace):
        url = node.text or ""
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "huwachongli.com":
            raise ValueError("Sitemap contains a non-canonical host or scheme")
        if parsed.query or parsed.fragment or not parsed.path.endswith("/"):
            raise ValueError("Sitemap contains a non-canonical page URL")
        if (scope == "root" and parsed.path == "/") or (
            scope == "project" and parsed.path.startswith(PREFIX)
        ):
            urls.append(url)
    urls = list(dict.fromkeys(urls))
    if not urls:
        raise ValueError("No canonical page URLs belong to this repository")
    return urls


def public_files(root, scope, urls):
    base = HOST if scope == "root" else HOST + PREFIX.rstrip("/")
    files = {}
    for name in ["sitemap.xml", "restaurant.json", "reputation.json", "robots.txt", "llms.txt"]:
        files[base + "/" + name] = (root / name).read_bytes()
    if scope == "root":
        files[HOST + "/feed.xml"] = (root / "feed.xml").read_bytes()
    for url in urls:
        relative = urllib.parse.urlsplit(url).path.removeprefix(PREFIX) if scope == "project" else ""
        files[url] = (root / relative / "index.html").read_bytes()
    return files


def deployed_run(runs, revision):
    matches = [
        run for run in runs
        if run.get("path") == PAGES_PATH
        and run.get("head_branch") == "main" and run.get("head_sha") == revision
    ]
    if not matches:
        return False
    latest = max(matches, key=lambda run: run["id"])
    if latest.get("status") != "completed":
        return False
    if latest.get("conclusion") != "success":
        raise RuntimeError("The matching Pages deployment did not succeed; no URLs submitted")
    return True


class Client:
    def __init__(self, repository):
        self.api_base = "https://api.github.com/repos/" + repository

    def request(self, url, *, github=False, payload=None):
        headers = {"User-Agent": "Huwa-Deployment-IndexNow/1.0"}
        if github:
            headers["Accept"] = "application/vnd.github+json"
            headers["X-GitHub-Api-Version"] = "2022-11-28"
            token = os.environ.get("GITHUB_TOKEN")
            if token:
                headers["Authorization"] = "Bearer " + token
        else:
            headers["Cache-Control"] = "no-cache"
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.geturl() != url:
                raise RuntimeError("Unexpected redirect while checking a canonical endpoint")
            return response.status, response.read()

    def api(self, path):
        return json.loads(self.request(self.api_base + path, github=True)[1])


def wait_until_public(client, revision, expected, attempts, interval, sleep=time.sleep):
    for attempt in range(1, attempts + 1):
        if client.api("/commits/main")["sha"] != revision:
            print("Superseded by a newer main revision; no URLs submitted")
            return False
        query = urllib.parse.urlencode({"branch": "main", "head_sha": revision, "per_page": 50})
        runs = client.api("/actions/runs?" + query)["workflow_runs"]
        if deployed_run(runs, revision):
            pending = []
            for url, content in expected.items():
                try:
                    status, live = client.request(url)
                    if status != 200 or live != content:
                        pending.append(urllib.parse.urlsplit(url).path)
                except (urllib.error.URLError, TimeoutError):
                    pending.append(urllib.parse.urlsplit(url).path)
            if not pending:
                if client.api("/commits/main")["sha"] == revision:
                    print(f"Pages deployment and {len(expected)} public files verified")
                    return True
                print("Superseded during verification; no URLs submitted")
                return False
            print(f"Waiting for public content ({attempt}/{attempts}): " + ", ".join(pending))
        else:
            print(f"Waiting for the matching Pages deployment ({attempt}/{attempts})")
        if attempt < attempts:
            sleep(interval)
    raise RuntimeError("Deployment or public content is not ready; no URLs submitted")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true", help="Verify without submitting")
    parser.add_argument("--attempts", type=int, default=24)
    parser.add_argument("--interval", type=int, default=20)
    args = parser.parse_args()
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    revision = os.environ.get("GITHUB_SHA", "")
    if repository not in REPOSITORIES or os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise ValueError("Only the two approved main-branch repositories may submit")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("A full deployment commit SHA is required")
    if args.attempts < 1 or args.interval < 0:
        raise ValueError("Invalid wait limits")
    root = Path(__file__).resolve().parents[2]
    scope = REPOSITORIES[repository]
    urls = canonical_urls((root / "sitemap.xml").read_bytes(), scope)
    expected = public_files(root, scope, urls)
    # IndexNow's ownership file is deliberately public, not an account credential.
    key_files = [file for file in root.glob("*.txt") if re.fullmatch(r"[0-9a-f]{32}", file.stem)]
    if len(key_files) != 1 or key_files[0].read_text().strip() != key_files[0].stem:
        raise ValueError("The public IndexNow ownership file is missing or inconsistent")
    key_file = key_files[0]
    key_location = HOST + "/" + key_file.name
    expected[key_location] = key_file.read_bytes()
    client = Client(repository)
    if not wait_until_public(client, revision, expected, args.attempts, args.interval):
        return
    if args.check_only:
        print(f"Read-only check passed; {len(urls)} {scope} URLs ready; nothing submitted")
        return
    status, _ = client.request("https://api.indexnow.org/indexnow", payload={
        "host": "huwachongli.com", "key": key_file.stem,
        "keyLocation": key_location, "urlList": urls,
    })
    if status not in (200, 202):
        raise RuntimeError(f"IndexNow did not accept the submission (HTTP {status})")
    print(f"IndexNow HTTP {status}; submitted {len(urls)} deployed {scope} URLs")


if __name__ == "__main__":
    main()
